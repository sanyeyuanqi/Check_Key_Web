"""Persistent, bounded export decryption workers; workers never access files."""

import asyncio
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import security
from .config import settings

_logger = logging.getLogger(__name__)
_pool: ProcessPoolExecutor | None = None
_parallelism = 0
_retired_pools: list[ProcessPoolExecutor] = []
_lifecycle_lock = asyncio.Lock()
_worker_cipher: Fernet | None = None
_worker_start_barrier: Any = None
_DECRYPT_ERROR = "密钥数据无法解密"


def _initialize_export_worker(key: bytes, start_barrier: Any) -> None:
    global _worker_cipher, _worker_start_barrier
    _worker_cipher = Fernet(key)
    _worker_start_barrier = start_barrier


def _warm_export_worker() -> None:
    # Each submitted task occupies one process until every initializer has run.
    _worker_start_barrier.wait(timeout=30)


def _decrypt_worker_block(ciphertexts: list[str]) -> bytes:
    if _worker_cipher is None:
        raise RuntimeError("导出解密服务尚未初始化")
    try:
        plaintexts = []
        for value in ciphertexts:
            plaintext = _worker_cipher.decrypt(value.encode("ascii"))
            plaintext.decode("utf-8")
            plaintexts.append(plaintext)
        return b"\n".join(plaintexts)
    except (InvalidToken, ValueError, UnicodeError, AttributeError, TypeError):
        raise ValueError(_DECRYPT_ERROR) from None


def _decrypt_thread_block(ciphertexts: list[str]) -> bytes:
    try:
        return "\n".join(security.decrypt_secret_batch(ciphertexts)).encode("utf-8")
    except (InvalidToken, ValueError, UnicodeError, AttributeError, TypeError):
        raise ValueError(_DECRYPT_ERROR) from None


async def _wait_for_completion(future: asyncio.Future):
    """Cancellation waits for submitted CPU work, keeping request slots bounded."""
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        # Consume any worker failure without replacing the cancellation signal.
        if future.done() and not future.cancelled():
            future.exception()
        raise


async def _thread_work(function, *args, **kwargs):
    return await _wait_for_completion(asyncio.create_task(asyncio.to_thread(function, *args, **kwargs)))


async def start_export_crypto_workers() -> None:
    global _pool, _parallelism
    async with _lifecycle_lock:
        if _pool is not None:
            return
        count = settings.export_crypto_workers
        if count <= 0:
            return
        context = multiprocessing.get_context("spawn")
        pool = None
        try:
            key = settings.fernet_key.encode("ascii")
            Fernet(key)  # Validate before spawning; child initializers never print key errors.
            barrier = context.Barrier(count)
            pool = ProcessPoolExecutor(
                max_workers=count, mp_context=context,
                initializer=_initialize_export_worker,
                initargs=(key, barrier),
            )
            loop = asyncio.get_running_loop()
            initialized = asyncio.gather(*[
                loop.run_in_executor(pool, _warm_export_worker) for _ in range(count)
            ])
            await _wait_for_completion(initialized)
        except BaseException as error:
            if pool is not None:
                await _thread_work(pool.shutdown, wait=True, cancel_futures=True)
            if not isinstance(error, Exception):
                raise
            # Never include configuration, ciphertexts or exception data in logs.
            _logger.warning("Parallel export workers unavailable; using the thread fallback")
            return
        _pool = pool
        _parallelism = count


async def close_export_crypto_workers() -> None:
    global _pool, _parallelism
    async with _lifecycle_lock:
        pool, _pool = _pool, None
        _parallelism = 0
        pools = ([pool] if pool is not None else []) + _retired_pools[:]
        _retired_pools.clear()
        if pools:
            closing = asyncio.gather(*[
                asyncio.to_thread(previous.shutdown, wait=True, cancel_futures=True)
                for previous in pools
            ])
            await _wait_for_completion(closing)


def export_crypto_parallelism() -> int:
    return _parallelism if _pool is not None else 0


async def decrypt_export_block(ciphertexts: list[str]) -> bytes:
    global _pool, _parallelism
    if not ciphertexts:
        return b""
    pool = _pool
    if pool is not None:
        try:
            future = asyncio.get_running_loop().run_in_executor(pool, _decrypt_worker_block, ciphertexts)
            return await _wait_for_completion(future)
        except BrokenProcessPool:
            if _pool is pool:
                _pool = None
                _parallelism = 0
                _retired_pools.append(pool)
                # Broken workers have already failed; shutdown rejects any further work.
                pool.shutdown(wait=False, cancel_futures=True)
                _logger.warning("Parallel export workers stopped; using the thread fallback")
    return await _thread_work(_decrypt_thread_block, ciphertexts)
