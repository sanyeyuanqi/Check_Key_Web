import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import DBAPIError, OperationalError

from app import worker
from app.bulk_ingest import QueuedResultJob, queue_observed_at
from app.schemas import ValidationResultInput
from app.security import encrypt_job


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.epoch = 1_700_000_000.0

    def monotonic(self):
        return self.elapsed

    def wall_time(self):
        return self.epoch + self.elapsed

    def advance(self, seconds):
        self.elapsed += seconds


def result_payload(index=1):
    return {
        'index': index, 'status': 'valid', 'usable': True,
        'credential': {'provider': 'OpenAI', 'kind': 'openai', 'secret': 'synthetic-worker-credential'},
    }


def message_id(clock, sequence=0, *, age=0):
    return f'{int((clock.wall_time() - age) * 1000)}-{sequence}'


def job(clock, count=1, sequence=0, *, batch_id=1, age=0):
    identifier = message_id(clock, sequence, age=age)
    return QueuedResultJob(
        message_id=identifier, batch_id=batch_id,
        results=[ValidationResultInput.model_validate(result_payload(i + 1)) for i in range(count)],
        source_ip=None, observed_at=queue_observed_at(identifier),
    )


def entry(clock, count=1, sequence=0, *, batch_id=1, age=0, extra=None):
    payload = {'batch_id': batch_id, 'results': [result_payload(i + 1) for i in range(count)]}
    payload.update(extra or {})
    return message_id(clock, sequence, age=age).encode(), {b'payload': encrypt_job(payload).encode()}


def postgres_error(state, *, field='sqlstate', invalidated=False):
    original = RuntimeError('synthetic-private-postgres-error')
    setattr(original, field, state)
    return DBAPIError('synthetic-sql', None, original, connection_invalidated=invalidated)


class FakeSessions:
    def __init__(self, events):
        self.events = events
        self.sessions = []

    @asynccontextmanager
    async def __call__(self):
        session = object()
        self.sessions.append(session)
        self.events.append(('open', session))
        try:
            yield session
        finally:
            self.events.append(('close', session))


def configured_worker(monkeypatch, clock, *, processor=None, redis=None):
    events = []
    sessions = FakeSessions(events)

    async def commit(_session, jobs):
        events.append(('commit', [item.message_id for item in jobs]))
        return sum(len(item.results) for item in jobs)

    async def finish(_redis, ids):
        events.append(('finish', ids))

    processor = processor or AsyncMock(side_effect=commit)
    finish_mock = AsyncMock(side_effect=finish)
    failures = AsyncMock()
    monkeypatch.setattr(worker, 'finish_messages', finish_mock)
    monkeypatch.setattr(worker, 'handle_failed_messages', failures)
    monkeypatch.setattr(worker.settings, 'queue_read_messages', 20)
    monkeypatch.setattr(worker.settings, 'queue_claim_idle_seconds', 120)
    redis = redis or SimpleNamespace(
        xpending_range=AsyncMock(return_value=[]), xclaim=AsyncMock(return_value=[]),
        xreadgroup=AsyncMock(return_value=[]),
    )
    instance = worker.BatchWorker(
        redis, 'synthetic-worker', session_factory=sessions, processor=processor,
        monotonic=clock.monotonic, wall_time=clock.wall_time,
        batch_records=1000, wait_seconds=30,
    )
    return instance, processor, finish_mock, failures, sessions, events


def test_buffer_counts_results_not_messages_at_999_and_1000():
    clock = FakeClock()
    buffer = worker.ResultBatchBuffer(1000, 30, monotonic=clock.monotonic, wall_time=clock.wall_time)
    for sequence in range(19):
        buffer.add(job(clock, 50, sequence))
    buffer.add(job(clock, 49, 19))
    assert len(buffer.jobs) == 20
    assert buffer.record_count == 999
    assert not buffer.due()
    buffer.add(job(clock, 1, 20))
    assert buffer.record_count == 1000
    assert buffer.due()
    assert len(buffer.take()) == 21
    assert buffer.record_count == 0 and buffer.deadline is None and not buffer.message_ids


def test_deadline_is_earliest_stream_age_and_never_reset_by_new_messages():
    clock = FakeClock()
    buffer = worker.ResultBatchBuffer(1000, 30, monotonic=clock.monotonic, wall_time=clock.wall_time)
    buffer.add(job(clock, age=25))
    assert buffer.deadline == 5
    clock.advance(4.999)
    buffer.add(job(clock, sequence=1))
    assert buffer.deadline == 5
    assert 1 <= buffer.read_block_ms() <= 2
    assert not buffer.due()
    clock.advance(.001)
    assert buffer.due()


def test_future_stream_time_still_waits_at_most_30_seconds_from_receipt():
    clock = FakeClock()
    buffer = worker.ResultBatchBuffer(1000, 30, monotonic=clock.monotonic, wall_time=clock.wall_time)
    buffer.add(job(clock, age=-100))
    assert buffer.deadline == 30
    clock.advance(30)
    assert buffer.due()


def test_duplicate_buffered_stream_id_cannot_double_count_results():
    clock = FakeClock()
    buffer = worker.ResultBatchBuffer(1000, 30, monotonic=clock.monotonic, wall_time=clock.wall_time)
    original = job(clock, 50)
    buffer.add(original)
    clock.advance(20)
    buffer.add(original)
    assert buffer.record_count == 50 and len(buffer.jobs) == 1 and buffer.deadline == 30


@pytest.mark.parametrize('extra,expected_ip', [({}, None), ({'source_ip': '2001:db8::7'}, '2001:db8::7')])
def test_decode_preserves_legacy_payloads_and_uses_exact_stream_time(extra, expected_ip):
    clock = FakeClock()
    extra = {**extra, 'enqueued_at': '2099-01-01T00:00:00+00:00'}
    identifier, fields = entry(clock, extra=extra)
    decoded = worker.decode_entry(identifier, fields)
    assert decoded.message_id == identifier.decode()
    assert decoded.source_ip == expected_ip
    assert decoded.observed_at == datetime.fromtimestamp(clock.wall_time(), UTC)
    assert decoded.results == [ValidationResultInput.model_validate(result_payload())]


@pytest.mark.asyncio
async def test_threshold_flushes_immediately_and_leaves_remaining_entries_for_next_batch(monkeypatch):
    clock = FakeClock()
    instance, processor, finish, failures, sessions, events = configured_worker(monkeypatch, clock)
    entries = [entry(clock, 50, sequence) for sequence in range(21)]
    await instance.process_entries(entries)
    processor.assert_awaited_once()
    assert sum(len(item.results) for item in processor.await_args.args[1]) == 1000
    assert len(processor.await_args.args[1]) == 20
    assert instance.buffer.record_count == 50
    assert [kind for kind, _ in events] == ['open', 'commit', 'close', 'finish']
    assert len(sessions.sessions) == 1
    finish.assert_awaited_once()
    failures.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_timeout_flushes_at_30_seconds_without_more_arrivals(monkeypatch):
    clock = FakeClock()
    reads = []

    async def read(*_args, **kwargs):
        reads.append(kwargs)
        if len(reads) == 1:
            return [('synthetic-stream', [entry(clock)])]
        if 'block' in kwargs:
            clock.advance(kwargs['block'] / 1000)
        return []

    redis = SimpleNamespace(xpending_range=AsyncMock(return_value=[]), xclaim=AsyncMock(), xreadgroup=read)
    instance, processor, finish, _failures, _sessions, _events = configured_worker(monkeypatch, clock, redis=redis)
    await instance.poll_once()
    for _ in range(5):
        await instance.poll_once()
    assert clock.elapsed == 25
    processor.assert_not_awaited()
    await instance.poll_once()
    assert clock.elapsed == 30
    processor.assert_awaited_once()
    finish.assert_awaited_once()
    assert all(item['count'] == 20 and ('block' not in item or 1 <= item['block'] <= 5000) for item in reads)
    assert 'block' not in reads[-1]


@pytest.mark.asyncio
async def test_continuous_small_arrivals_cannot_postpone_oldest_deadline(monkeypatch):
    clock = FakeClock()
    sequence = 0

    async def read(*_args, **kwargs):
        nonlocal sequence
        if 'block' not in kwargs:
            return []  # No already-ready backlog between these one-second arrivals.
        clock.advance(min(1, kwargs['block'] / 1000))
        sequence += 1
        return [('synthetic-stream', [entry(clock, sequence=sequence)])]

    redis = SimpleNamespace(xpending_range=AsyncMock(return_value=[]), xclaim=AsyncMock(), xreadgroup=read)
    instance, processor, finish, _failures, _sessions, _events = configured_worker(monkeypatch, clock, redis=redis)
    for _ in range(30):
        await instance.poll_once()
    processor.assert_not_awaited()
    await instance.poll_once()
    assert clock.elapsed == 31
    assert len(processor.await_args.args[1]) == 31
    finish.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('backlog_size', [999, 1000])
async def test_expired_single_record_backlog_drains_to_one_batch_or_ready_empty(monkeypatch, backlog_size):
    clock = FakeClock()
    ready = deque(entry(clock, sequence=sequence, age=60) for sequence in range(backlog_size))
    reads = []

    async def read(*_args, **kwargs):
        reads.append(kwargs)
        entries = [ready.popleft() for _ in range(min(kwargs['count'], len(ready)))]
        return [('synthetic-stream', entries)] if entries else []

    redis = SimpleNamespace(xpending_range=AsyncMock(return_value=[]), xclaim=AsyncMock(), xreadgroup=read)
    instance, processor, finish, failures, sessions, _events = configured_worker(monkeypatch, clock, redis=redis)
    await instance.poll_once()
    processor.assert_awaited_once()
    assert sum(len(item.results) for item in processor.await_args.args[1]) == backlog_size
    assert clock.elapsed == 0 and not ready and not instance.buffer.jobs
    assert len(sessions.sessions) == 1
    finish.assert_awaited_once()
    failures.assert_not_awaited()
    assert reads[0]['block'] == 5000
    assert all(item['count'] == 20 and 'block' not in item for item in reads[1:])
    assert len(reads) == (50 if backlog_size == 1000 else 51)


@pytest.mark.asyncio
async def test_expired_drain_stops_after_threshold_attempt_even_with_continuous_ready_arrivals(monkeypatch):
    clock = FakeClock()
    reads = 0

    async def read(*_args, **kwargs):
        nonlocal reads
        offset = reads * kwargs['count']
        reads += 1
        return [('synthetic-stream', [entry(clock, sequence=offset + i, age=60) for i in range(kwargs['count'])])]

    redis = SimpleNamespace(xpending_range=AsyncMock(return_value=[]), xclaim=AsyncMock(), xreadgroup=read)
    instance, processor, finish, failures, _sessions, _events = configured_worker(
        monkeypatch, clock, redis=redis, processor=AsyncMock(side_effect=RuntimeError('synthetic-write-failure')),
    )
    await instance.poll_once()
    assert reads == 50 and instance.flush_generation == 1
    processor.assert_awaited_once()
    assert len(processor.await_args.args[1]) == 1000
    finish.assert_not_awaited()
    failures.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_drain_counts_duplicate_messages_in_its_work_budget(monkeypatch):
    clock = FakeClock()
    duplicate = entry(clock, age=60)
    redis = SimpleNamespace(
        xpending_range=AsyncMock(return_value=[]), xclaim=AsyncMock(),
        xreadgroup=AsyncMock(return_value=[('synthetic-stream', [duplicate] * 20)]),
    )
    instance, processor, _finish, _failures, _sessions, _events = configured_worker(monkeypatch, clock, redis=redis)
    instance.buffer.add(worker.decode_entry(*duplicate))
    await instance.poll_once()
    assert redis.xreadgroup.await_count == 50
    assert all('block' not in call.kwargs for call in redis.xreadgroup.await_args_list)
    processor.assert_awaited_once()
    assert len(processor.await_args.args[1]) == 1


@pytest.mark.asyncio
async def test_empty_queue_never_opens_database_or_acknowledges(monkeypatch):
    clock = FakeClock()
    instance, processor, finish, failures, sessions, _events = configured_worker(monkeypatch, clock)
    for _ in range(10):
        clock.advance(5)
        await instance.poll_once()
    assert not await instance.flush()
    assert not sessions.sessions
    processor.assert_not_awaited()
    finish.assert_not_awaited()
    failures.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_write_failure_leaves_pending_and_logs_no_payload(monkeypatch, capsys):
    clock = FakeClock()
    secret = 'synthetic-private-exception-value'
    processor = AsyncMock(side_effect=RuntimeError(secret))
    instance, _processor, finish, failures, sessions, _events = configured_worker(monkeypatch, clock, processor=processor)
    instance.buffer.add(job(clock))
    assert not await instance.flush()
    finish.assert_not_awaited()
    failures.assert_awaited_once_with(instance.redis, [message_id(clock)], error_type='RuntimeError')
    assert len(sessions.sessions) == 1 and not instance.buffer.jobs
    output = capsys.readouterr().out
    assert 'RuntimeError' in output and message_id(clock) in output
    assert secret not in output and 'synthetic-worker-credential' not in output


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [OperationalError('synthetic-sql', None, Exception('synthetic-private')), TimeoutError('synthetic-private')])
async def test_infrastructure_failure_never_acks_or_exhausts_job_retry_limit(monkeypatch, error):
    clock = FakeClock()
    instance, _processor, finish, failures, _sessions, _events = configured_worker(monkeypatch, clock, processor=AsyncMock(side_effect=error))
    instance.buffer.add(job(clock))
    assert not await instance.flush()
    finish.assert_not_awaited()
    failures.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [ValueError('synthetic-bad-data'), HTTPException(404, 'synthetic-bad-batch')])
async def test_poisoned_job_isolated_with_fresh_sessions_and_only_bad_job_retried(monkeypatch, failure):
    clock = FakeClock()

    async def process(_session, jobs):
        if any(item.batch_id == 2 for item in jobs):
            raise failure
        return sum(len(item.results) for item in jobs)

    instance, processor, finish, failures, sessions, _events = configured_worker(monkeypatch, clock, processor=AsyncMock(side_effect=process))
    good, bad = job(clock, sequence=1), job(clock, sequence=2, batch_id=2)
    instance.buffer.add(good)
    instance.buffer.add(bad)
    assert not await instance.flush()
    assert processor.await_count == 3 and len(set(sessions.sessions)) == 3
    finish.assert_awaited_once_with(instance.redis, [good.message_id])
    failures.assert_awaited_once_with(instance.redis, [bad.message_id], error_type=type(failure).__name__)


@pytest.mark.asyncio
@pytest.mark.parametrize('state,field', [('22001', 'sqlstate'), ('22P02', 'pgcode'), ('23505', 'sqlstate')])
async def test_generic_asyncpg_dbapi_data_errors_isolate_only_bad_jobs(monkeypatch, state, field):
    clock = FakeClock()
    failure = postgres_error(state, field=field)

    async def process(_session, jobs):
        if any(item.batch_id == 2 for item in jobs):
            raise failure
        return sum(len(item.results) for item in jobs)

    instance, processor, finish, failures, sessions, _events = configured_worker(monkeypatch, clock, processor=AsyncMock(side_effect=process))
    good, bad = job(clock, sequence=1), job(clock, sequence=2, batch_id=2)
    instance.buffer.add(good)
    instance.buffer.add(bad)
    assert not await instance.flush()
    assert processor.await_count == 3 and len(set(sessions.sessions)) == 3
    finish.assert_awaited_once_with(instance.redis, [good.message_id])
    failures.assert_awaited_once_with(instance.redis, [bad.message_id], error_type='DBAPIError')


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['08006', '40001', '40P01', '53100', '53200', '53300', '57P01', '57P02', '57P03'])
async def test_generic_asyncpg_dbapi_transient_errors_leave_entire_batch_pending(monkeypatch, state):
    clock = FakeClock()
    instance, processor, finish, failures, sessions, _events = configured_worker(
        monkeypatch, clock, processor=AsyncMock(side_effect=postgres_error(state)),
    )
    instance.buffer.add(job(clock, sequence=1))
    instance.buffer.add(job(clock, sequence=2))
    assert not await instance.flush()
    processor.assert_awaited_once()
    assert len(sessions.sessions) == 1
    finish.assert_not_awaited()
    failures.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalidated_dbapi_connection_takes_precedence_over_data_sqlstate(monkeypatch):
    clock = FakeClock()
    instance, processor, finish, failures, _sessions, _events = configured_worker(
        monkeypatch, clock, processor=AsyncMock(side_effect=postgres_error('22001', invalidated=True)),
    )
    instance.buffer.add(job(clock, sequence=1))
    instance.buffer.add(job(clock, sequence=2))
    assert not await instance.flush()
    processor.assert_awaited_once()
    finish.assert_not_awaited()
    failures.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_dbapi_errors_still_use_bounded_retry_instead_of_unlimited_infrastructure_retry(monkeypatch):
    clock = FakeClock()
    instance, processor, finish, failures, _sessions, _events = configured_worker(
        monkeypatch, clock, processor=AsyncMock(side_effect=postgres_error('XX000')),
    )
    original = job(clock)
    instance.buffer.add(original)
    assert not await instance.flush()
    processor.assert_awaited_once()
    finish.assert_not_awaited()
    failures.assert_awaited_once_with(instance.redis, [original.message_id], error_type='DBAPIError')


@pytest.mark.asyncio
async def test_acknowledgement_failure_after_commit_does_not_dead_letter_valid_jobs(monkeypatch):
    clock = FakeClock()
    instance, processor, finish, failures, _sessions, _events = configured_worker(monkeypatch, clock)
    finish.side_effect = ConnectionError('synthetic-private')
    instance.buffer.add(job(clock))
    assert not await instance.flush()
    processor.assert_awaited_once()
    failures.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_encrypted_message_is_isolated_without_affecting_valid_buffer(monkeypatch, capsys):
    clock = FakeClock()
    instance, processor, finish, failures, _sessions, _events = configured_worker(monkeypatch, clock)
    malformed_id = message_id(clock, 1)
    await instance.process_entries([
        entry(clock, sequence=0), (malformed_id.encode(), {b'payload': b'synthetic-secret-bad-ciphertext'}),
        entry(clock, sequence=2),
    ])
    assert instance.buffer.record_count == 2
    processor.assert_not_awaited()
    finish.assert_not_awaited()
    assert failures.await_args.args == (instance.redis, [malformed_id])
    assert 'synthetic-secret-bad-ciphertext' not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_reclaim_waits_120_seconds_and_never_claims_buffered_jobs(monkeypatch):
    clock = FakeClock()
    instance, _processor, _finish, _failures, _sessions, _events = configured_worker(monkeypatch, clock)
    buffered = job(clock, sequence=0)
    claimed = entry(clock, sequence=1)
    instance.buffer.add(buffered)
    instance.redis.xpending_range.return_value = [
        {'message_id': buffered.message_id.encode()}, {'message_id': claimed[0]},
    ]
    instance.redis.xclaim.return_value = [claimed]
    await instance.reclaim_stale()
    assert instance.redis.xpending_range.await_args.kwargs['idle'] == 120_000
    assert instance.redis.xclaim.await_args.kwargs['min_idle_time'] == 120_000
    assert instance.redis.xclaim.await_args.kwargs['message_ids'] == [claimed[0].decode()]
    assert instance.buffer.record_count == 2


@pytest.mark.asyncio
async def test_expired_pending_backlog_fills_one_batch_and_reads_new_flow_without_blocking(monkeypatch):
    clock = FakeClock()
    pending = deque(entry(clock, sequence=sequence, age=180) for sequence in range(1000))
    by_id = {identifier.decode(): (identifier, fields) for identifier, fields in pending}
    cursors = []

    async def pending_range(*_args, **kwargs):
        cursors.append(kwargs['min'])
        page = [pending.popleft() for _ in range(min(kwargs['count'], len(pending)))]
        return [{'message_id': identifier} for identifier, _fields in page]

    async def claim(*_args, **kwargs):
        return [by_id[identifier] for identifier in kwargs['message_ids']]

    fresh = entry(clock, sequence=1001)
    redis = SimpleNamespace(
        xpending_range=AsyncMock(side_effect=pending_range), xclaim=AsyncMock(side_effect=claim),
        xreadgroup=AsyncMock(return_value=[('synthetic-stream', [fresh])]),
    )
    instance, processor, finish, failures, _sessions, _events = configured_worker(monkeypatch, clock, redis=redis)
    await instance.poll_once()
    processor.assert_awaited_once()
    assert len(processor.await_args.args[1]) == 1000 and not pending
    assert len(cursors) == 50 and cursors[0] == '-'
    assert all(cursor.startswith('(') for cursor in cursors[1:])
    assert instance.buffer.record_count == 1 and instance.buffer.jobs[0].message_id == fresh[0].decode()
    assert instance.next_reclaim_at == clock.monotonic()
    redis.xreadgroup.assert_awaited_once()
    assert 'block' not in redis.xreadgroup.await_args.kwargs
    finish.assert_awaited_once()
    failures.assert_not_awaited()


@pytest.mark.asyncio
async def test_reclaim_scans_past_a_full_page_of_its_own_buffered_pending_messages(monkeypatch):
    clock = FakeClock()
    entries = [entry(clock, sequence=sequence, age=180) for sequence in range(40)]
    pending = deque(entries)
    by_id = {identifier.decode(): (identifier, fields) for identifier, fields in entries}

    async def pending_range(*_args, **kwargs):
        page = [pending.popleft() for _ in range(min(kwargs['count'], len(pending)))]
        return [{'message_id': identifier} for identifier, _fields in page]

    async def claim(*_args, **kwargs):
        return [by_id[identifier] for identifier in kwargs['message_ids']]

    redis = SimpleNamespace(
        xpending_range=AsyncMock(side_effect=pending_range), xclaim=AsyncMock(side_effect=claim),
        xreadgroup=AsyncMock(return_value=[]),
    )
    instance, processor, _finish, _failures, _sessions, _events = configured_worker(monkeypatch, clock, redis=redis)
    await instance.process_entries(entries[:20])
    await instance.poll_once()
    processor.assert_awaited_once()
    assert len(processor.await_args.args[1]) == 40
    assert redis.xpending_range.await_args_list[1].kwargs['min'] == f'({entries[19][0].decode()}'
    redis.xclaim.assert_awaited_once()
    assert redis.xclaim.await_args.kwargs['message_ids'] == [identifier.decode() for identifier, _fields in entries[20:]]


@pytest.mark.asyncio
async def test_graceful_stop_flushes_below_threshold(monkeypatch):
    clock = FakeClock()
    instance, processor, finish, _failures, _sessions, _events = configured_worker(monkeypatch, clock)
    instance.buffer.add(job(clock))
    stop = asyncio.Event()
    stop.set()
    await instance.run(stop)
    processor.assert_awaited_once()
    finish.assert_awaited_once()
    instance.redis.xreadgroup.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_leaves_uncommitted_buffer_pending(monkeypatch):
    clock = FakeClock()
    instance, processor, finish, failures, _sessions, _events = configured_worker(monkeypatch, clock)
    instance.buffer.add(job(clock))
    instance.poll_once = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await instance.run(asyncio.Event())
    processor.assert_not_awaited()
    finish.assert_not_awaited()
    failures.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_read_failure_preserves_buffer_and_recovers_without_restart(monkeypatch, capsys):
    clock = FakeClock()
    instance, processor, finish, failures, _sessions, _events = configured_worker(monkeypatch, clock)
    original = job(clock)
    instance.buffer.add(original)
    stop = asyncio.Event()
    polls = 0

    async def poll():
        nonlocal polls
        polls += 1
        assert instance.buffer.jobs == [original]
        if polls == 1:
            raise RedisConnectionError('synthetic-private-redis-error')
        stop.set()

    instance.poll_once = poll
    pause = AsyncMock()
    monkeypatch.setattr(worker, '_wait_for_stop', pause)
    await instance.run(stop)
    assert polls == 2
    pause.assert_awaited_once_with(stop)
    processor.assert_awaited_once()
    finish.assert_awaited_once()
    failures.assert_not_awaited()
    output = capsys.readouterr().out
    assert 'ConnectionError' in output and 'synthetic-private-redis-error' not in output


@pytest.mark.asyncio
async def test_non_redis_poll_failures_raise_without_losing_pending_jobs(monkeypatch):
    clock = FakeClock()
    instance, processor, finish, _failures, _sessions, _events = configured_worker(monkeypatch, clock)
    original = job(clock)
    instance.buffer.add(original)
    instance.poll_once = AsyncMock(side_effect=RuntimeError('synthetic-internal-error'))
    with pytest.raises(RuntimeError, match='synthetic-internal-error'):
        await instance.run(asyncio.Event())
    assert instance.buffer.jobs == [original]
    processor.assert_not_awaited()
    finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_backoff_is_immediately_interruptible_by_stop():
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(worker._wait_for_stop(stop), timeout=.1)


def test_failure_logs_cap_identifiers_and_never_print_exception_payload(capsys):
    identifiers = [f'1700000000000-{sequence}' for sequence in range(1000)]
    worker._log_failure('write', ValueError('synthetic-private-error'), identifiers)
    output = capsys.readouterr().out
    assert 'count=1000' in output and identifiers[9] in output
    assert f'{identifiers[10]},' not in output
    assert 'synthetic-private-error' not in output


@pytest.mark.asyncio
async def test_run_closes_cache_and_redis_on_cancellation_even_if_cache_close_fails(monkeypatch):
    redis = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(worker, 'redis_client', lambda: redis)
    monkeypatch.setattr(worker, 'ensure_consumer_group', AsyncMock())
    monkeypatch.setattr(worker, 'BatchWorker', lambda *_args: SimpleNamespace(run=AsyncMock(side_effect=asyncio.CancelledError())))
    close_cache = AsyncMock(side_effect=RuntimeError('synthetic-close-failure'))
    monkeypatch.setattr(worker, 'close_query_cache', close_cache)
    with pytest.raises(RuntimeError, match='synthetic-close-failure'):
        await worker.run(asyncio.Event())
    close_cache.assert_awaited_once()
    redis.aclose.assert_awaited_once()
