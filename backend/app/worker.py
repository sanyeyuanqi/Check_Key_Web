import asyncio
import math
import os
import re
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import (
    DataError,
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from .bulk_ingest import QueuedResultJob, process_queued_results, queue_observed_at
from .config import settings
from .database import SessionFactory
from .query_cache import close_query_cache
from .queue import (
    ensure_consumer_group,
    finish_messages,
    handle_failed_messages,
    redis_client,
)
from .schemas import BatchCompleteInput
from .security import decrypt_job


def _message_value(fields: dict[Any, Any], key: str) -> Any:
    return fields.get(key) or fields.get(key.encode())


def _stream_id(value: str | bytes) -> str:
    result = value.decode('ascii') if isinstance(value, bytes) else str(value)
    if not re.fullmatch(r'\d{1,20}-\d{1,20}', result):
        raise ValueError('Invalid Redis stream ID')
    return result


def decode_entry(message_id: str | bytes, fields: dict[Any, Any]) -> QueuedResultJob:
    identifier = _stream_id(message_id)
    payload_value = _message_value(fields, 'payload')
    if not payload_value:
        raise ValueError('Missing encrypted queue payload')
    payload = decrypt_job(payload_value)
    batch_id = int(str(payload['batch_id']))
    if batch_id < 1:
        raise ValueError('Invalid batch ID')
    results = BatchCompleteInput.model_validate({'results': payload['results']}).results
    source_ip = payload.get('source_ip')
    if source_ip is not None and not isinstance(source_ip, str):
        raise ValueError('Invalid source IP type')
    # Stream time survives retries and is authoritative even for older payloads.
    return QueuedResultJob(
        message_id=identifier, batch_id=batch_id, results=results,
        source_ip=source_ip, observed_at=queue_observed_at(identifier),
    )


def _log_failure(stage: str, error: BaseException, identifiers: list[str | bytes]) -> None:
    safe_ids = []
    for identifier in identifiers[:10]:
        try:
            safe_ids.append(_stream_id(identifier))
        except (UnicodeError, ValueError):
            safe_ids.append('<invalid-id>')
    # Never interpolate the exception message, decrypted results or credentials.
    print(f"queue {stage} ids={','.join(safe_ids)} count={len(identifiers)} type={type(error).__name__}", flush=True)


@dataclass
class ResultBatchBuffer:
    max_records: int
    max_wait_seconds: float
    monotonic: Callable[[], float] = time.monotonic
    wall_time: Callable[[], float] = time.time
    jobs: list[QueuedResultJob] = field(default_factory=list)
    record_count: int = 0
    deadline: float | None = None
    message_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.max_records < 1 or self.max_wait_seconds <= 0:
            raise ValueError('Batch limits must be positive')

    def add(self, job: QueuedResultJob) -> None:
        if job.message_id in self.message_ids:
            return
        if not job.results:
            raise ValueError('Empty queue job')
        age = max(0.0, self.wall_time() - job.observed_at.timestamp())
        deadline = self.monotonic() + max(0.0, self.max_wait_seconds - age)
        self.deadline = deadline if self.deadline is None else min(self.deadline, deadline)
        self.jobs.append(job)
        self.message_ids.add(job.message_id)
        self.record_count += len(job.results)

    def due(self) -> bool:
        return bool(self.jobs) and (
            self.record_count >= self.max_records
            or (self.deadline is not None and self.monotonic() >= self.deadline)
        )

    def read_block_ms(self, maximum: int = 5000) -> int:
        if self.deadline is None:
            return maximum
        return max(1, min(maximum, math.ceil((self.deadline - self.monotonic()) * 1000)))

    def take(self) -> list[QueuedResultJob]:
        jobs = self.jobs
        self.jobs = []
        self.record_count = 0
        self.deadline = None
        self.message_ids.clear()
        return jobs


def _postgres_sqlstate(error: Exception) -> str | None:
    if not isinstance(error, DBAPIError):
        return None
    state = getattr(error.orig, 'sqlstate', None) or getattr(error.orig, 'pgcode', None)
    return state if isinstance(state, str) else None


def _transient_sqlstate(state: str | None) -> bool:
    return bool(state) and (state[:2] in ('08', '40', '53') or state in ('57P01', '57P02', '57P03'))


def _data_error(error: Exception) -> bool:
    state = _postgres_sqlstate(error)
    if isinstance(error, DBAPIError) and (error.connection_invalidated or _transient_sqlstate(state)):
        return False
    if state is not None and state[:2] in ('22', '23'):
        return True
    return isinstance(error, (ValueError, IntegrityError, DataError)) or (
        isinstance(error, HTTPException) and error.status_code in (400, 404, 422)
    )


def _infrastructure_error(error: Exception) -> bool:
    state = _postgres_sqlstate(error)
    if isinstance(error, DBAPIError):
        if error.connection_invalidated or _transient_sqlstate(state):
            return True
        if state is not None and state[:2] in ('22', '23'):
            return False
    return isinstance(error, (OperationalError, InterfaceError, SQLAlchemyTimeoutError, TimeoutError, OSError, RedisError)) or (
        isinstance(error, HTTPException) and error.status_code >= 500
    )


async def _wait_for_stop(stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=1)
    except TimeoutError:
        pass


class BatchWorker:
    def __init__(
        self, redis: Redis, consumer_name: str, *,
        session_factory: Callable[[], Any] | None = None,
        processor: Callable[[Any, list[QueuedResultJob]], Awaitable[int]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        batch_records: int | None = None,
        wait_seconds: float | None = None,
    ) -> None:
        self.redis = redis
        self.consumer_name = consumer_name
        self.session_factory = session_factory or SessionFactory
        self.processor = processor or process_queued_results
        self.monotonic = monotonic
        self.buffer = ResultBatchBuffer(
            settings.queue_batch_records if batch_records is None else batch_records,
            settings.queue_batch_wait_seconds if wait_seconds is None else wait_seconds,
            monotonic=monotonic, wall_time=wall_time,
        )
        self.next_reclaim_at = 0.0
        self.flush_generation = 0

    async def _record_failure(self, identifiers: list[str], error: Exception) -> None:
        try:
            await handle_failed_messages(self.redis, identifiers, error_type=type(error).__name__)
        except Exception as failure:  # noqa: BLE001 - Redis errors leave the original task pending.
            _log_failure('retry', failure, identifiers)

    async def _write_jobs(self, jobs: list[QueuedResultJob]) -> None:
        async with self.session_factory() as session:
            # The processor commits the entire transaction before returning.
            await self.processor(session, jobs)

    async def _finish_jobs(self, jobs: list[QueuedResultJob]) -> bool:
        identifiers = [job.message_id for job in jobs]
        try:
            await finish_messages(self.redis, identifiers)
            return True
        except Exception as error:  # noqa: BLE001 - Preserve pending jobs after any acknowledgement failure.
            # DB already committed: a Redis outage must never exhaust job retries.
            _log_failure('finish', error, identifiers)
            return False

    async def _flush_jobs(self, jobs: list[QueuedResultJob], *, isolate: bool = True) -> bool:
        identifiers = [job.message_id for job in jobs]
        try:
            await self._write_jobs(jobs)
        except Exception as error:  # noqa: BLE001 - Isolate bad jobs without leaking payload-bearing errors.
            _log_failure('write', error, identifiers)
            if isolate and len(jobs) > 1 and _data_error(error):
                # A poisoned job cannot exhaust retries for otherwise valid jobs.
                results = [await self._flush_jobs([job], isolate=False) for job in jobs]
                return all(results)
            if not _infrastructure_error(error):
                await self._record_failure(identifiers, error)
            return False
        return await self._finish_jobs(jobs)

    async def flush(self) -> bool:
        if not self.buffer.jobs:
            return False
        # A failed transaction or ACK still ends this drain's batch attempt.
        self.flush_generation += 1
        return await self._flush_jobs(self.buffer.take())

    async def flush_if_due(self) -> bool:
        return await self.flush() if self.buffer.due() else False

    async def process_entries(self, entries: list[tuple[Any, dict[Any, Any]]]) -> None:
        generation = self.flush_generation
        for message_id, fields in entries:
            try:
                job = decode_entry(message_id, fields)
            except Exception as error:  # noqa: BLE001 - Malformed encrypted tasks are isolated and retained.
                _log_failure('decode', error, [message_id])
                try:
                    identifier = _stream_id(message_id)
                except (UnicodeError, ValueError):
                    continue  # Redis itself never returns malformed stream IDs.
                await self._record_failure([identifier], error)
                continue
            self.buffer.add(job)
            if self.buffer.record_count >= self.buffer.max_records and self.flush_generation == generation:
                await self.flush()

    async def reclaim_stale(self) -> None:
        idle_ms = max(120, settings.queue_claim_idle_seconds) * 1000
        generation = self.flush_generation
        scanned = 0
        cursor = '-'
        seen: set[str] = set()
        while self.flush_generation == generation and scanned < self.buffer.max_records:
            pending = await self.redis.xpending_range(
                settings.redis_stream_name, settings.redis_consumer_group,
                min=cursor, max='+', count=settings.queue_read_messages, idle=idle_ms,
            )
            if not pending:
                break
            scanned += len(pending)
            identifiers = [_stream_id(_message_value(item, 'message_id')) for item in pending]
            cursor = f'({identifiers[-1]}'
            unseen = [identifier for identifier in identifiers if identifier not in seen]
            if not unseen:
                break
            seen.update(unseen)
            eligible = [identifier for identifier in unseen if identifier not in self.buffer.message_ids]
            if eligible:
                claimed = await self.redis.xclaim(
                    settings.redis_stream_name, settings.redis_consumer_group, self.consumer_name,
                    min_idle_time=idle_ms, message_ids=eligible,
                )
                await self.process_entries(claimed)
            if len(pending) < settings.queue_read_messages:
                break

    async def _read_messages(self, *, block_ms: int | None = None) -> int:
        options = {'count': settings.queue_read_messages}
        if block_ms is not None:
            options['block'] = block_ms
        # Omit block for ready-only reads: Redis interprets block=0 as infinite.
        streams = await self.redis.xreadgroup(
            settings.redis_consumer_group, self.consumer_name,
            {settings.redis_stream_name: '>'}, **options,
        )
        count = 0
        for _stream_name, entries in streams:
            count += len(entries)
            await self.process_entries(entries)
        return count

    async def _flush_due_after_drain(self) -> bool:
        if not self.buffer.due():
            return False
        generation = self.flush_generation
        scanned = 0
        # Old backlog can already exceed the wait limit on receipt. Fill one
        # batch from ready messages before committing a smaller expired batch.
        # Count every fetched message so poisoned/duplicate jobs cannot spin.
        while self.buffer.record_count < self.buffer.max_records and scanned < self.buffer.max_records:
            count = await self._read_messages()
            scanned += count
            if self.flush_generation != generation:
                return True
            if not count:
                break
        await self.flush()
        return True

    async def poll_once(self) -> None:
        # A small configured threshold can leave a full buffer after the one
        # permitted commit in the preceding read. Flush it before reading more.
        if self.buffer.record_count >= self.buffer.max_records:
            await self.flush()
            return
        generation = self.flush_generation
        if self.monotonic() >= self.next_reclaim_at:
            await self.reclaim_stale()
            if self.flush_generation != generation:
                self.next_reclaim_at = self.monotonic()
                # Keep old pending recovery fast while giving new arrivals one
                # ready-only read; blocking here would delay the next old batch.
                await self._read_messages()
                return
            self.next_reclaim_at = self.monotonic() + 30
        if await self._flush_due_after_drain():
            return
        await self._read_messages(block_ms=self.buffer.read_block_ms())
        if self.flush_generation == generation:
            await self._flush_due_after_drain()

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except RedisError as error:
                _log_failure('redis', error, [job.message_id for job in self.buffer.jobs])
                await _wait_for_stop(stop_event)
        await self.flush()


async def run(stop_event: asyncio.Event | None = None) -> None:
    redis = redis_client()
    consumer_name = f'{socket.gethostname()}-{os.getpid()}'
    stop = stop_event if stop_event is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    signal_installed = False
    try:
        await ensure_consumer_group(redis)
        if stop_event is None:
            try:
                loop.add_signal_handler(signal.SIGTERM, stop.set)
                signal_installed = True
            except (NotImplementedError, RuntimeError):
                pass  # Cancellation/Windows shutdown leaves uncommitted jobs pending.
        await BatchWorker(redis, consumer_name).run(stop)
    finally:
        if signal_installed:
            loop.remove_signal_handler(signal.SIGTERM)
        try:
            await close_query_cache()
        finally:
            await redis.aclose()


if __name__ == '__main__':
    asyncio.run(run())
