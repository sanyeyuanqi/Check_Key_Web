"""Bounded, private temporary files for exports; plaintext never enters Redis."""

import asyncio
import hashlib
import os
import secrets
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from starlette.responses import FileResponse

from .config import settings
from .export_crypto import decrypt_export_block, export_crypto_parallelism
from .security import decrypt_secret_batch, hash_token


async def _file_work(function: Callable, *args):
    # A cancelled request must wait for its in-flight writer before deleting the file.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    try:
        value = task.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    if cancelled:
        raise asyncio.CancelledError
    return value


@dataclass
class ExportFile:
    path: Path
    manager: "ExportFiles"
    count: int = 0
    size: int = 0
    disposed: bool = False

    def append(self, values: Iterable[str]) -> None:
        values = list(values)
        if not values:
            return
        self.append_block("\n".join(values).encode("utf-8"), len(values))

    def append_block(self, block: bytes, count: int) -> None:
        if count == 0:
            return
        if self.count:
            block = b"\n" + block
        if self.size + len(block) > settings.export_max_file_bytes:
            raise HTTPException(413, "导出文件过大，请缩小导出范围后重试")
        self.manager.reserve(self, len(block))
        with self.path.open("ab") as output:
            output.write(block)
        self.count += count

    def append_encrypted(self, ciphertexts: list[str]) -> None:
        self.append(decrypt_secret_batch(ciphertexts))

    def dispose(self) -> None:
        self.manager.dispose(self)


@dataclass
class DownloadTicket:
    file: ExportFile
    file_name: str
    user_id: int
    session_hash: str
    expires_at: float


class PrivateFileResponse(FileResponse):
    chunk_size = 1024 * 1024

    def __init__(self, file: ExportFile, file_name: str):
        self.export_file = file
        super().__init__(
            file.path, filename=file_name, media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"},
        )
        self.headers["Accept-Ranges"] = "none"

    async def __call__(self, scope, receive, send) -> None:
        try:
            # A one-use ticket always returns the complete file, never a consumed partial download.
            scope = {**scope, "headers": [(name, value) for name, value in scope.get("headers", []) if name.lower() not in {b"range", b"if-range"}]}
            await super().__call__(scope, receive, send)
        finally:
            await _file_work(self.export_file.dispose)


class ExportFiles:
    def __init__(self):
        self._directory: tempfile.TemporaryDirectory | None = None
        self._lock = threading.RLock()
        self._writers = 0
        self._files: dict[Path, ExportFile] = {}
        self._tickets: dict[str, DownloadTicket] = {}
        self._bytes = 0

    @asynccontextmanager
    async def writer(self) -> AsyncIterator[None]:
        await _file_work(self.prune)
        with self._lock:
            if self._writers >= settings.export_max_concurrent or len(self._files) >= settings.export_max_pending_files:
                raise HTTPException(503, "当前导出任务较多，请稍后重试", headers={"Retry-After": "5"})
            self._writers += 1
        try:
            yield
        finally:
            with self._lock:
                self._writers -= 1

    def create(self) -> ExportFile:
        with self._lock:
            if len(self._files) >= settings.export_max_pending_files:
                raise HTTPException(503, "待下载文件较多，请稍后重试", headers={"Retry-After": "5"})
            if self._directory is None:
                self._directory = tempfile.TemporaryDirectory(prefix="key-control-exports-")
                (Path(self._directory.name) / ".export-owner").write_text("key-control-private-exports-v1", encoding="ascii")
            # NamedTemporaryFile creates a mode-0600 file inside a mode-0700 directory.
            with tempfile.NamedTemporaryFile(dir=self._directory.name, suffix=".txt", delete=False) as output:
                file = ExportFile(Path(output.name), self)
            self._files[file.path] = file
            return file

    def reserve(self, file: ExportFile, size: int) -> None:
        with self._lock:
            if file.disposed or file.path not in self._files:
                raise RuntimeError("Export file already closed")
            if self._bytes + size > settings.export_max_total_bytes:
                raise HTTPException(503, "导出临时空间繁忙，请稍后重试", headers={"Retry-After": "5"})
            self._bytes += size
            file.size += size

    def dispose(self, file: ExportFile) -> None:
        with self._lock:
            if file.disposed:
                return
            file.path.unlink(missing_ok=True)
            file.disposed = True
            self._files.pop(file.path, None)
            self._bytes -= file.size

    def register(self, file: ExportFile, *, file_name: str, user_id: int, session_token: str) -> dict[str, Any]:
        token = secrets.token_hex(32)
        with self._lock:
            self._tickets[hashlib.sha256(token.encode()).hexdigest()] = DownloadTicket(
                file, file_name, user_id, hash_token(session_token), time.monotonic() + settings.export_download_ttl_seconds,
            )
        return {
            "downloadUrl": f"/api/exports/download/{token}", "fileName": file_name,
            "count": file.count, "bytes": file.size, "expiresIn": settings.export_download_ttl_seconds,
        }

    def claim(self, token: str, *, user_id: int, session_token: str) -> DownloadTicket:
        key = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            ticket = self._tickets.get(key)
            if ticket is None or ticket.user_id != user_id or not secrets.compare_digest(ticket.session_hash, hash_token(session_token)):
                raise HTTPException(404, "下载链接无效或已过期，请重新导出")
            if ticket.expires_at <= time.monotonic():
                self._tickets.pop(key)
                ticket.file.dispose()
                raise HTTPException(404, "下载链接无效或已过期，请重新导出")
            # Only one authenticated download can claim this file.
            return self._tickets.pop(key)

    def prune(self) -> None:
        with self._lock:
            if self._directory is not None:
                os.utime(self._directory.name, None)
            expired = [key for key, ticket in self._tickets.items() if ticket.expires_at <= time.monotonic()]
            for key in expired:
                self._tickets.pop(key).file.dispose()
            # Crashed processes leave no in-memory tickets. Only remove our marked,
            # inactive temp directories; live instances renew their directory lease.
            root = Path(tempfile.gettempdir()).resolve()
            for directory in root.glob("key-control-exports-*"):
                try:
                    if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != root:
                        continue
                    if time.time() - directory.stat().st_mtime <= max(120, settings.export_download_ttl_seconds):
                        continue
                    marker = directory / ".export-owner"
                    if marker.is_symlink() or marker.stat().st_size > 64 or marker.read_text(encoding="ascii") != "key-control-private-exports-v1":
                        continue
                    contents = list(directory.iterdir())
                    if any(item.is_symlink() or not item.is_file() or (item != marker and item.suffix != ".txt") for item in contents):
                        continue
                    for item in contents:
                        item.unlink(missing_ok=True)
                    directory.rmdir()
                except (OSError, UnicodeError):
                    continue

    def close(self) -> None:
        with self._lock:
            self._tickets.clear()
            for file in list(self._files.values()):
                file.dispose()
            if self._directory is not None:
                self._directory.cleanup()
                self._directory = None


export_files = ExportFiles()


async def sweep_export_files() -> None:
    while True:
        await asyncio.sleep(30)
        await _file_work(export_files.prune)


async def _write_parallel_batches(file: ExportFile, result, request: Request, parallelism: int) -> None:
    partitions = result.partitions(settings.export_batch_size).__aiter__()
    pending: deque[tuple[asyncio.Task, int]] = deque()
    active: set[asyncio.Task] = set()
    exhausted = False

    async def submit_next() -> None:
        nonlocal exhausted
        if exhausted:
            return
        ciphertexts = await anext(partitions, None)
        if ciphertexts is None:
            exhausted = True
            return
        if await request.is_disconnected():
            raise HTTPException(499, "导出请求已取消")
        task = asyncio.create_task(decrypt_export_block(list(ciphertexts)))
        active.add(task)
        pending.append((task, len(ciphertexts)))

    try:
        for _ in range(parallelism):
            await submit_next()
            if exhausted:
                break
        while pending:
            task, count = pending.popleft()
            block = await task
            active.discard(task)
            if await request.is_disconnected():
                raise HTTPException(499, "导出请求已取消")
            await _file_work(file.append_block, block, count)
            await submit_next()
        if await request.is_disconnected():
            raise HTTPException(499, "导出请求已取消")
    finally:
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)


async def write_query_export(session, statement, request: Request) -> ExportFile:
    file = export_files.create()
    result = None
    try:
        result = await session.stream_scalars(statement.execution_options(yield_per=settings.export_batch_size))
        pending_write = None
        try:
            parallelism = export_crypto_parallelism()
            if parallelism > 0:
                await _write_parallel_batches(file, result, request, parallelism)
                return file
            partitions = result.partitions(settings.export_batch_size).__aiter__()
            ciphertexts = await anext(partitions, None)
            while ciphertexts is not None:
                if await request.is_disconnected():
                    raise HTTPException(499, "导出请求已取消")
                pending_write = asyncio.create_task(_file_work(file.append_encrypted, list(ciphertexts)))
                # Fetch just one batch ahead while the current batch decrypts and writes.
                following = await anext(partitions, None)
                await pending_write
                pending_write = None
                ciphertexts = following
            if await request.is_disconnected():
                raise HTTPException(499, "导出请求已取消")
        finally:
            try:
                if pending_write is not None:
                    await pending_write
            finally:
                await result.close()
        return file
    except BaseException:
        await _file_work(file.dispose)
        raise


async def write_selected_export(values: list[str]) -> ExportFile:
    file = export_files.create()
    try:
        await _file_work(file.append, values)
        return file
    except BaseException:
        await _file_work(file.dispose)
        raise
